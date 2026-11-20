"""A new agent webhook endpoint can verify signatures from its first delivery.

Before: every signing secret was minted silently (the legacy agents.webhook_url migration, the first
PUT, the emit path) and only its last4 was ever returned. The only full secret anyone saw came from a
rotation, and that one stayed PENDING for 24 h while deliveries were still signed with the unseen
secret. A new endpoint could not verify a single signature for about a day.

Now:
  * the first PUT that mints the secret returns it in full, once (`config.signing_secret`), and
    stamps `secret_revealed_at`; no later PUT, GET, delivery list, test send, managed-inbox row or
    log line carries it;
  * rotating a secret nobody has seen makes the new one current at once; rotating a shown one keeps
    the 24 h overlap;
  * the emit-path and legacy mints never replace a stored (let alone a shown) secret.

These run the REAL service functions against a throwaway SQLite file; only outbound HTTP and the
destination DNS check are stubbed.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from databases import Database

from services import agent_webhook_service as svc

AGENT = "agent_placeholder_001"
DEST = "https://receiver.example/hooks/pivota"
EVENT = svc.DEFAULT_WEBHOOK_EVENTS[0]


class _FakeHttp:
    """Replaces httpx.AsyncClient: records each POST (url, headers, body) and answers 200."""

    posts: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        _FakeHttp.posts.append({"url": url, "headers": dict(headers or {}), "body": content})

        class _Resp:
            status_code = 200
            text = "ok"

        return _Resp()


async def _no_summary(agent_id):
    return {"total": 0, "succeeded": 0, "failed": 0, "retrying": 0, "success_rate": 0.0, "last_delivery_at": None}


@asynccontextmanager
async def _service_db(tmp_path, monkeypatch):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'webhooks.db'}")
    await db.connect()
    try:
        monkeypatch.setattr(svc, "database", db)
        monkeypatch.setattr(svc, "_AGENT_WEBHOOK_DDL_READY", False)
        monkeypatch.setattr(svc.webhook_destination_guard, "check_destination_url", lambda url: url)
        monkeypatch.setattr(svc.httpx, "AsyncClient", _FakeHttp)
        # get_delivery_summary is Postgres SQL (`::int`); its numbers are not what these pin.
        monkeypatch.setattr(svc, "get_delivery_summary", _no_summary)
        monkeypatch.setenv("PIVOTA_ENV", "production")
        monkeypatch.delenv("WEBHOOK_DELIVERY_ENABLED", raising=False)
        _FakeHttp.posts = []
        await db.execute("CREATE TABLE agents (agent_id VARCHAR(255) PRIMARY KEY, webhook_url TEXT)")
        await svc.ensure_agent_webhook_tables()
        yield db
    finally:
        await db.disconnect()


async def _row(db) -> dict:
    row = await db.fetch_one("SELECT * FROM agent_webhook_configs WHERE agent_id = :a", {"a": AGENT})
    return dict(row) if row else {}


async def _put(**overrides):
    kwargs = {"enabled": True, "destination_url": DEST, "subscribed_events": [EVENT]}
    kwargs.update(overrides)
    return await svc.update_webhook_config(AGENT, **kwargs)


def _assert_signed_with(post: dict, secret: str) -> None:
    timestamp = post["headers"]["X-Pivota-Timestamp"]
    assert post["headers"]["X-Pivota-Signature"] == svc._build_signature(secret, timestamp, post["body"])


def _close_to(iso: str, expected: datetime, slack_seconds: int = 60) -> bool:
    got = datetime.fromisoformat(iso)
    return abs((got - expected).total_seconds()) <= slack_seconds


# ── (a) the first PUT shows the secret once ───────────────────────────────────────────────────


async def test_the_first_put_returns_the_secret_once_and_a_second_put_does_not(tmp_path, monkeypatch):
    async with _service_db(tmp_path, monkeypatch) as db:
        first = await _put()
        secret = first["signing_secret"]
        assert secret.startswith("whsec_")
        assert first["signing_secret_last4"] == secret[-4:]
        assert first["secret_revealed_at"] is not None
        assert first["rotation_activation"] == svc.ROTATION_OVERLAP

        stored = await _row(db)
        assert stored["signing_secret"] == secret
        assert stored["secret_revealed_at"] is not None

        second = await _put(subscribed_events=list(svc.DEFAULT_WEBHOOK_EVENTS))
        assert "signing_secret" not in second
        assert second["signing_secret_last4"] == secret[-4:]
        assert (await _row(db))["signing_secret"] == secret  # unchanged


async def test_the_first_put_on_a_legacy_row_shows_nothing_and_its_rotation_is_immediate(
    tmp_path, monkeypatch
):
    """A legacy-migration mint is never shown by a PUT (the PUT did not create it)."""
    async with _service_db(tmp_path, monkeypatch) as db:
        await db.execute("INSERT INTO agents (agent_id, webhook_url) VALUES (:a, :u)", {"a": AGENT, "u": DEST})
        config = await svc.get_webhook_config(AGENT)
        legacy_secret = (await _row(db))["signing_secret"]
        assert legacy_secret
        assert config["migration_source"] == "agents.webhook_url"
        assert config["secret_revealed_at"] is None
        assert config["rotation_activation"] == svc.ROTATION_IMMEDIATE

        put = await _put()
        assert "signing_secret" not in put
        assert put["secret_revealed_at"] is None
        assert (await _row(db))["signing_secret"] == legacy_secret


async def test_a_concurrent_mint_wins_and_the_put_then_shows_nothing(tmp_path, monkeypatch):
    """The PUT read "no secret", then another writer stored one first: the PUT must not show
    its own (unstored) secret, nor stamp the other one as shown."""
    async with _service_db(tmp_path, monkeypatch) as db:
        real_get = svc._get_or_create_raw_config
        raced = []

        async def stale_read(agent_id):
            config = await real_get(agent_id)
            if raced:
                return config
            raced.append(True)
            await db.execute(
                "INSERT INTO agent_webhook_configs (agent_id, signing_secret, subscribed_events, created_at, updated_at)"
                " VALUES (:a, 'whsec_other_writer', '[]', :t, :t)",
                {"a": agent_id, "t": svc._db_now()},
            )
            return config

        monkeypatch.setattr(svc, "_get_or_create_raw_config", stale_read)
        put = await _put()
        monkeypatch.setattr(svc, "_get_or_create_raw_config", real_get)

        assert "signing_secret" not in put
        stored = await _row(db)
        assert stored["signing_secret"] == "whsec_other_writer"
        assert stored["secret_revealed_at"] is None


async def test_a_concurrent_mint_on_an_existing_secretless_row_is_not_stamped_as_shown(
    tmp_path, monkeypatch
):
    """The same race on the UPDATE branch: the row exists with no secret, another writer stores
    one between the PUT's read and its write."""
    async with _service_db(tmp_path, monkeypatch) as db:
        await db.execute(
            "INSERT INTO agent_webhook_configs (agent_id, subscribed_events, created_at, updated_at)"
            " VALUES (:a, '[]', :t, :t)",
            {"a": AGENT, "t": svc._db_now()},
        )
        real_get = svc._get_or_create_raw_config
        raced = []

        async def stale_read(agent_id):
            config = await real_get(agent_id)
            if not raced:
                raced.append(True)
                await db.execute(
                    "UPDATE agent_webhook_configs SET signing_secret = 'whsec_other_writer' WHERE agent_id = :a",
                    {"a": agent_id},
                )
            return config

        monkeypatch.setattr(svc, "_get_or_create_raw_config", stale_read)
        put = await _put()

        assert "signing_secret" not in put
        stored = await _row(db)
        assert stored["signing_secret"] == "whsec_other_writer"
        assert stored["secret_revealed_at"] is None
        assert stored["destination_url"] == DEST  # the PUT's own fields still landed


async def test_the_first_put_on_an_existing_secretless_row_shows_the_secret(tmp_path, monkeypatch):
    async with _service_db(tmp_path, monkeypatch) as db:
        await db.execute(
            "INSERT INTO agent_webhook_configs (agent_id, subscribed_events, created_at, updated_at)"
            " VALUES (:a, '[]', :t, :t)",
            {"a": AGENT, "t": svc._db_now()},
        )
        put = await _put()
        stored = await _row(db)
        assert put["signing_secret"] == stored["signing_secret"]
        assert stored["secret_revealed_at"] is not None


# ── (b) rotation: immediate when never shown, 24 h overlap when shown ───────────────────────


async def test_rotating_an_unrevealed_secret_is_immediate_and_the_next_delivery_uses_it(
    tmp_path, monkeypatch
):
    async with _service_db(tmp_path, monkeypatch) as db:
        await db.execute("INSERT INTO agents (agent_id, webhook_url) VALUES (:a, :u)", {"a": AGENT, "u": DEST})
        await svc.get_webhook_config(AGENT)  # legacy mint: never shown
        before = datetime.now(timezone.utc)

        result = await svc.rotate_signing_secret(AGENT)
        new_secret = result["new_signing_secret"]
        assert result["activation"] == svc.ROTATION_IMMEDIATE
        assert _close_to(result["activates_at"], before)
        assert result["activates_at"] == result["overlap_ends_at"]
        assert result["current_signing_secret_last4"] == new_secret[-4:]
        assert result["pending_signing_secret_last4"] is None

        stored = await _row(db)
        assert stored["signing_secret"] == new_secret
        assert stored["pending_signing_secret"] is None
        assert stored["pending_secret_activates_at"] is None
        assert stored["secret_revealed_at"] is not None

        config = await svc.get_webhook_config(AGENT)
        assert config["signing_secret_last4"] == new_secret[-4:]
        assert config["pending_signing_secret_last4"] is None
        assert config["rotation_activation"] == svc.ROTATION_OVERLAP  # it has been shown now

        await svc.emit_agent_webhook_event(AGENT, event_type=EVENT, payload={"n": 1})
        assert len(_FakeHttp.posts) == 1
        _assert_signed_with(_FakeHttp.posts[0], new_secret)


async def test_rotating_an_unrevealed_secret_drops_a_pending_one(tmp_path, monkeypatch):
    """A row rotated before this change: current unseen, pending shown. A new rotation replaces
    both at once, and nothing keeps signing with the unseen one."""
    async with _service_db(tmp_path, monkeypatch) as db:
        now = svc._db_now()
        await db.execute(
            "INSERT INTO agent_webhook_configs (agent_id, enabled, destination_url, subscribed_events,"
            " signing_secret, pending_signing_secret, pending_secret_activates_at, created_at, updated_at)"
            " VALUES (:a, TRUE, :u, :e, 'whsec_unseen_current', 'whsec_old_pending', :p, :t, :t)",
            {"a": AGENT, "u": DEST, "e": json.dumps([EVENT]), "p": now + timedelta(hours=20), "t": now},
        )
        result = await svc.rotate_signing_secret(AGENT)
        assert result["activation"] == svc.ROTATION_IMMEDIATE
        stored = await _row(db)
        assert stored["signing_secret"] == result["new_signing_secret"]
        assert stored["pending_signing_secret"] is None


async def test_rotating_with_no_config_row_is_immediate(tmp_path, monkeypatch):
    async with _service_db(tmp_path, monkeypatch) as db:
        result = await svc.rotate_signing_secret(AGENT)
        assert result["activation"] == svc.ROTATION_IMMEDIATE
        stored = await _row(db)
        assert stored["signing_secret"] == result["new_signing_secret"]
        assert stored["pending_signing_secret"] is None
        assert stored["secret_revealed_at"] is not None


async def test_rotating_a_revealed_secret_keeps_the_24h_overlap(tmp_path, monkeypatch):
    async with _service_db(tmp_path, monkeypatch) as db:
        first_secret = (await _put())["signing_secret"]
        before = datetime.now(timezone.utc)

        result = await svc.rotate_signing_secret(AGENT)
        new_secret = result["new_signing_secret"]
        assert result["activation"] == svc.ROTATION_OVERLAP
        assert _close_to(result["activates_at"], before + timedelta(hours=24))
        assert result["current_signing_secret_last4"] == first_secret[-4:]
        assert result["pending_signing_secret_last4"] == new_secret[-4:]

        stored = await _row(db)
        assert stored["signing_secret"] == first_secret
        assert stored["pending_signing_secret"] == new_secret

        # Deliveries keep the shown secret until the overlap ends.
        await svc.emit_agent_webhook_event(AGENT, event_type=EVENT, payload={"n": 1})
        _assert_signed_with(_FakeHttp.posts[-1], first_secret)

        # A second rotation inside the overlap is still an overlap (the current one was shown).
        again = await svc.rotate_signing_secret(AGENT)
        assert again["activation"] == svc.ROTATION_OVERLAP
        assert (await _row(db))["signing_secret"] == first_secret


async def test_a_stale_unrevealed_read_cannot_make_a_shown_secret_rotate_immediately(
    tmp_path, monkeypatch
):
    """The immediate replacement is claimed in SQL: a rotation that read the row before another
    one stamped it falls through to the overlap instead of replacing the shown secret."""
    async with _service_db(tmp_path, monkeypatch) as db:
        shown = (await _put())["signing_secret"]
        stale = dict(await _row(db))
        stale["secret_revealed_at"] = None

        async def stale_read(agent_id):
            return dict(stale)

        monkeypatch.setattr(svc, "_get_or_create_raw_config", stale_read)
        result = await svc.rotate_signing_secret(AGENT)
        assert result["activation"] == svc.ROTATION_OVERLAP
        assert (await _row(db))["signing_secret"] == shown


# ── the emit-path mint ────────────────────────────────────────────────────────────────────────


async def test_a_skipped_event_mints_no_secret(tmp_path, monkeypatch):
    async with _service_db(tmp_path, monkeypatch) as db:
        result = await svc.emit_agent_webhook_event(AGENT, event_type=EVENT, payload={"n": 1})
        assert result["reason"] == "webhook_not_configured"
        assert await _row(db) == {}
        # So the developer's first PUT is the one that mints, and shows, the secret.
        assert (await _put())["signing_secret"].startswith("whsec_")


async def test_the_emit_mint_signs_with_the_stored_secret_and_rotates_immediately(tmp_path, monkeypatch):
    async with _service_db(tmp_path, monkeypatch) as db:
        now = svc._db_now()
        await db.execute(
            "INSERT INTO agent_webhook_configs (agent_id, enabled, destination_url, subscribed_events,"
            " created_at, updated_at) VALUES (:a, TRUE, :u, :e, :t, :t)",
            {"a": AGENT, "u": DEST, "e": json.dumps([EVENT]), "t": now},
        )
        await svc.emit_agent_webhook_event(AGENT, event_type=EVENT, payload={"n": 1})
        stored = await _row(db)
        assert stored["signing_secret"]
        assert stored["secret_revealed_at"] is None
        _assert_signed_with(_FakeHttp.posts[-1], stored["signing_secret"])

        assert (await svc.rotate_signing_secret(AGENT))["activation"] == svc.ROTATION_IMMEDIATE


async def test_emit_and_legacy_paths_never_replace_a_shown_secret(tmp_path, monkeypatch):
    async with _service_db(tmp_path, monkeypatch) as db:
        await db.execute("INSERT INTO agents (agent_id, webhook_url) VALUES (:a, :u)", {"a": AGENT, "u": DEST})
        await db.execute(
            "INSERT INTO agent_webhook_configs (agent_id, enabled, destination_url, subscribed_events,"
            " created_at, updated_at) VALUES (:a, TRUE, :u, :e, :t, :t)",
            {"a": AGENT, "u": DEST, "e": json.dumps([EVENT]), "t": svc._db_now()},
        )
        shown = (await _put())["signing_secret"]

        await svc.get_webhook_config(AGENT)  # the legacy migration path
        await svc.emit_agent_webhook_event(AGENT, event_type=EVENT, payload={"n": 1})
        assert (await _row(db))["signing_secret"] == shown
        _assert_signed_with(_FakeHttp.posts[-1], shown)


# ── the secret appears nowhere else ─────────────────────────────────────────────────────────


async def test_the_secret_is_in_no_other_response_and_no_log_line(tmp_path, monkeypatch, caplog):
    # Every application logger down to DEBUG. The DB driver's own DEBUG query tracing
    # (`databases`, `aiosqlite`: every bound parameter of every query) is excluded; it is off in
    # every deployed service (root logger at WARNING) and predates this change.
    caplog.set_level(logging.DEBUG)
    # caplog.set_level restores each level at teardown (a level left behind breaks other files).
    for driver in ("databases", "aiosqlite", "asyncpg", "sqlalchemy"):
        caplog.set_level(logging.WARNING, logger=driver)
    async with _service_db(tmp_path, monkeypatch) as db:
        secret = (await _put())["signing_secret"]
        rotated = (await svc.rotate_signing_secret(AGENT))["new_signing_secret"]

        responses = [
            await _put(),
            await svc.get_webhook_config(AGENT),
            await svc.emit_agent_webhook_event(AGENT, event_type=EVENT, payload={"n": 1}),
            await svc.send_test_webhook(AGENT),
            await svc.list_deliveries(AGENT),
        ]
        # The managed inbox, with a delivery signed by the current secret.
        post = _FakeHttp.posts[-1]
        responses.append(
            await svc.receive_managed_inbox_delivery(
                AGENT, raw_body=post["body"], headers=post["headers"], source_ip=None, user_agent=None
            )
        )
        inbox_rows = await db.fetch_all("SELECT * FROM agent_webhook_managed_inbox_events")
        delivery_rows = await db.fetch_all("SELECT * FROM agent_webhook_deliveries")

        blob = json.dumps(responses, default=str) + json.dumps(
            [dict(r) for r in inbox_rows] + [dict(r) for r in delivery_rows], default=str
        )
        for value in (secret, rotated):
            assert value not in blob
            assert value not in caplog.text
        assert all("signing_secret" not in r for r in responses[:2])
