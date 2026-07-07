from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from services import partner_invite_email as mod


pytestmark = pytest.mark.asyncio


@dataclass
class _FakeResult:
    ok: bool
    error: str | None = None


class _FakeDb:
    def __init__(self, *, partner: dict | None, contact: dict | None) -> None:
        self._partner = partner
        self._contact = contact

    async def fetch_one(self, query: str, values: dict[str, Any] | None = None):
        sql = " ".join(query.split()).lower()
        if "from channel_partners" in sql:
            return self._partner
        if "from partner_contacts" in sql:
            return self._contact
        raise AssertionError(f"Unhandled query: {query}")


def _install(monkeypatch, *, partner, contact, sender):
    monkeypatch.setattr(mod, "database", _FakeDb(partner=partner, contact=contact))
    monkeypatch.setattr(mod, "send_email", sender)


async def test_sends_when_contact_email_present(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def _sender(**kwargs: Any) -> _FakeResult:
        calls.append(kwargs)
        return _FakeResult(ok=True)

    _install(
        monkeypatch,
        partner={"legal_name": "Markato Limited"},
        contact={"contact_email": "finance@markato.com", "contact_name": "Jo"},
        sender=_sender,
    )

    out = await mod.send_invite_email(
        channel_partner_id=19,
        signup_url="https://app.pivota.cc/signup?ref=mkto_abc",
        expires_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    assert out == {"email_sent": True, "recipient": "finance@markato.com"}
    assert len(calls) == 1
    assert calls[0]["to_email"] == "finance@markato.com"
    # The link and the multi-use explanation ride in the body.
    assert "mkto_abc" in calls[0]["text_body"]
    assert "same link works for every brand" in calls[0]["text_body"]
    assert "2026-09-01" in calls[0]["text_body"]


async def test_no_contact_email_skips_send(monkeypatch) -> None:
    def _sender(**kwargs: Any) -> _FakeResult:
        raise AssertionError("send_email should not be called")

    _install(
        monkeypatch,
        partner={"legal_name": "Markato Limited"},
        contact={"contact_email": "  ", "contact_name": None},
        sender=_sender,
    )

    out = await mod.send_invite_email(
        channel_partner_id=19,
        signup_url="https://app.pivota.cc/signup?ref=x",
        expires_at=None,
    )
    assert out == {"email_sent": False, "reason": "no_contact_email"}


async def test_partner_not_found(monkeypatch) -> None:
    _install(
        monkeypatch,
        partner=None,
        contact=None,
        sender=lambda **k: _FakeResult(ok=True),
    )
    out = await mod.send_invite_email(
        channel_partner_id=999, signup_url="u", expires_at=None
    )
    assert out == {"email_sent": False, "reason": "partner_not_found"}


async def test_provider_failure_reported(monkeypatch) -> None:
    _install(
        monkeypatch,
        partner={"legal_name": "Markato"},
        contact={"contact_email": "a@b.com", "contact_name": None},
        sender=lambda **k: _FakeResult(ok=False, error="smtp_timeout"),
    )
    out = await mod.send_invite_email(
        channel_partner_id=19, signup_url="u", expires_at=None
    )
    assert out["email_sent"] is False
    assert out["recipient"] == "a@b.com"
    assert out["reason"] == "smtp_timeout"


async def test_sender_exception_is_swallowed(monkeypatch) -> None:
    def _boom(**kwargs: Any):
        raise RuntimeError("provider exploded")

    _install(
        monkeypatch,
        partner={"legal_name": "Markato"},
        contact={"contact_email": "a@b.com", "contact_name": None},
        sender=_boom,
    )
    out = await mod.send_invite_email(
        channel_partner_id=19, signup_url="u", expires_at=None
    )
    assert out == {"email_sent": False, "reason": "exception"}
