"""ACP delegate ALLOWANCE registry (services/acp_delegate_allowance_service).

The registry is the local, fail-closed record of what a delegated ACP token is
allowed to spend. These tests pin the two things that make it worth having over
the retired pivota-acp service's version:

  * mint-time validation — an unenforceable allowance (no/past expiry, a reason
    we do not support) cannot be recorded at all;
  * the single-use CAS — a token binds to exactly ONE checkout session, forever,
    even under concurrency, while remaining idempotent for that same session so
    a retry or a stale resume is never refused by its own earlier bind.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from db.acp_delegate_allowances import acp_delegate_allowances  # noqa: E402
from db.database import database, engine, metadata  # noqa: E402
from services import acp_delegate_allowance_service as reg  # noqa: E402


@pytest.fixture(autouse=True)
async def _db():
    metadata.create_all(engine, tables=[acp_delegate_allowances])
    if not database.is_connected:
        await database.connect()
    await reg._ensure_acp_delegate_allowances_table()
    await database.execute(acp_delegate_allowances.delete())
    yield


def _future(seconds: int = 900) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


async def _mint(**overrides):
    kwargs = dict(
        checkout_session_id="csn_abc123",
        merchant_id="merch_x",
        max_amount=4599,
        currency="USD",
        expires_at=_future(),
    )
    kwargs.update(overrides)
    return await reg.mint_allowance(**kwargs)


# --- mint --------------------------------------------------------------------


async def test_mint_generates_a_wire_parity_token_id():
    out = await _mint()
    token_id = out["token_id"]
    # `vt_` + 14 hex — the retired service's exact format, which external
    # platforms may have coded against.
    assert token_id.startswith("vt_")
    assert len(token_id) == len("vt_") + 14
    assert all(c in "0123456789abcdef" for c in token_id[len("vt_"):])
    assert reg.is_delegate_token(token_id)


async def test_mint_persists_and_reads_back():
    out = await _mint(token_id="vt_fixed00000001")
    stored = await reg.get_allowance("vt_fixed00000001")
    assert stored is not None
    assert stored["token_id"] == "vt_fixed00000001"
    assert stored["checkout_session_id"] == "csn_abc123"
    assert stored["merchant_id"] == "merch_x"
    assert stored["max_amount"] == 4599
    assert stored["currency"] == "USD"
    assert stored["reason"] == "one_time"
    assert stored["used"] is False
    assert stored["used_at"] is None
    assert stored["used_by_session"] is None
    assert stored["expires_at"] > datetime.now(timezone.utc)
    assert out["token_id"] == stored["token_id"]


async def test_get_allowance_miss_is_none():
    assert await reg.get_allowance("vt_never_minted") is None
    assert await reg.get_allowance("") is None
    assert await reg.get_allowance(None) is None


async def test_mint_rejects_a_missing_expiry():
    with pytest.raises(reg.AcpDelegateAllowanceError) as ei:
        await _mint(expires_at=None)
    assert ei.value.code == "invalid_allowance"
    assert "expires_at is required" in ei.value.message


async def test_mint_rejects_an_already_past_expiry():
    # An allowance that is born expired is unenforceable-by-accident: it would
    # sit in the registry looking valid to anything that does not check.
    with pytest.raises(reg.AcpDelegateAllowanceError) as ei:
        await _mint(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert ei.value.code == "invalid_allowance"
    assert "future" in ei.value.message


async def test_mint_rejects_an_unsupported_reason():
    with pytest.raises(reg.AcpDelegateAllowanceError) as ei:
        await _mint(reason="recurring")
    assert ei.value.code == "invalid_allowance"
    assert "one_time" in ei.value.message
    # ...and nothing was recorded.
    rows = await database.fetch_all(acp_delegate_allowances.select())
    assert list(rows) == []


async def test_mint_rejects_blank_scope_and_negative_amount():
    for kwargs in (
        {"checkout_session_id": "  "},
        {"merchant_id": ""},
        {"currency": " "},
        {"max_amount": -1},
    ):
        with pytest.raises(reg.AcpDelegateAllowanceError) as ei:
            await _mint(**kwargs)
        assert ei.value.code == "invalid_allowance", kwargs


async def test_mint_accepts_a_zero_amount_allowance():
    # 0 is a legal (if useless) cap; only NEGATIVE is malformed.
    out = await _mint(max_amount=0)
    assert (await reg.get_allowance(out["token_id"]))["max_amount"] == 0


# --- the single-use CAS ------------------------------------------------------


async def test_bind_marks_the_allowance_consumed():
    out = await _mint()
    assert await reg.bind_allowance_to_session(
        token_id=out["token_id"], session_id="csn_abc123"
    ) is True
    stored = await reg.get_allowance(out["token_id"])
    assert stored["used"] is True
    assert stored["used_by_session"] == "csn_abc123"
    assert stored["used_at"] is not None


async def test_same_session_rebind_is_idempotent():
    # A completion retry (or a stale resume of this session's own attempt) must
    # not be refused by the bind it wrote itself — that would wedge a session
    # that may already have money in flight.
    out = await _mint()
    assert await reg.bind_allowance_to_session(
        token_id=out["token_id"], session_id="csn_abc123"
    ) is True
    for _ in range(3):
        assert await reg.bind_allowance_to_session(
            token_id=out["token_id"], session_id="csn_abc123"
        ) is True
    stored = await reg.get_allowance(out["token_id"])
    assert stored["used_by_session"] == "csn_abc123"


async def test_a_token_bound_to_another_session_refuses():
    out = await _mint()
    assert await reg.bind_allowance_to_session(
        token_id=out["token_id"], session_id="csn_first"
    ) is True
    assert await reg.bind_allowance_to_session(
        token_id=out["token_id"], session_id="csn_second"
    ) is False
    # ...and the first session still holds it (a refused bind changes nothing).
    stored = await reg.get_allowance(out["token_id"])
    assert stored["used_by_session"] == "csn_first"


async def test_bind_of_an_unknown_token_is_false():
    assert await reg.bind_allowance_to_session(
        token_id="vt_nope", session_id="csn_abc123"
    ) is False


async def test_bind_with_blank_arguments_is_false():
    out = await _mint()
    assert await reg.bind_allowance_to_session(token_id="", session_id="csn_x") is False
    assert await reg.bind_allowance_to_session(
        token_id=out["token_id"], session_id=""
    ) is False


async def test_concurrent_binds_from_two_sessions_exactly_one_wins():
    out = await _mint()
    results = await asyncio.gather(
        reg.bind_allowance_to_session(token_id=out["token_id"], session_id="csn_a"),
        reg.bind_allowance_to_session(token_id=out["token_id"], session_id="csn_b"),
    )
    assert sorted(results) == [False, True], results
    stored = await reg.get_allowance(out["token_id"])
    assert stored["used"] is True
    assert stored["used_by_session"] in {"csn_a", "csn_b"}


async def test_concurrent_binds_from_the_same_session_both_win():
    out = await _mint()
    results = await asyncio.gather(
        reg.bind_allowance_to_session(token_id=out["token_id"], session_id="csn_same"),
        reg.bind_allowance_to_session(token_id=out["token_id"], session_id="csn_same"),
    )
    assert results == [True, True]


# --- the token gate ----------------------------------------------------------


def test_is_delegate_token_is_exactly_the_vt_prefix():
    assert reg.is_delegate_token("vt_abc") is True
    assert reg.is_delegate_token("pm_card_visa") is False
    assert reg.is_delegate_token("spt_future") is False
    assert reg.is_delegate_token("tok_test") is False
    assert reg.is_delegate_token("") is False
    assert reg.is_delegate_token(None) is False


# --- no cardholder data, by construction -------------------------------------


def test_registry_table_has_no_card_columns():
    # The retired service stored raw PAN + CVC in a JSONB payload. This schema
    # must not even be ABLE to. (The Postgres gate asserts the same thing
    # against information_schema on the real dialect.)
    forbidden = ("number", "cvc", "cvv", "pan", "cryptogram", "payload")
    for column in acp_delegate_allowances.columns:
        name = column.name.lower()
        assert not any(bad in name for bad in forbidden), name
